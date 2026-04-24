from __future__ import annotations

import difflib
import hashlib
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Settings
from .security import SecurityError, matches_any_glob, normalize_rel_path, rel_posix, resolve_repo_path


class WritePolicyError(ValueError):
    """Raised when a write request violates the repository write policy."""


class StaleWriteError(ValueError):
    """Raised when expected_sha256 does not match the current file contents."""


class PatchApplyError(ValueError):
    """Raised when a unified diff cannot be validated or applied."""


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def current_text_sha256(path: str, settings: Settings) -> str:
    target, _ = resolve_write_path(path, settings)
    return sha256_text(_read_existing_text(target, settings))


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


def _read_optional_text(path: Path, settings: Settings) -> str | None:
    return _read_existing_text(path, settings) if path.exists() else None


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


def _ensure_move_delete_allowed(settings: Settings) -> None:
    if not settings.allow_move_delete_operations:
        raise WritePolicyError("move/delete operations are disabled by policy")


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


def resolve_write_dir_path(path: str, settings: Settings, *, create_if_missing: bool = True) -> tuple[Path, str]:
    root = settings.project_root.resolve()
    target = (root / path).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise SecurityError(f"path escapes repository root: {path}") from exc
    rel = rel_posix(root, target) if target.exists() else normalize_rel_path(path)
    if not rel or rel == ".":
        raise WritePolicyError("directory path must not be repository root")
    if target.exists() and not target.is_dir():
        raise WritePolicyError(f"not a directory: {rel}")
    if not target.exists() and not create_if_missing:
        raise FileNotFoundError(f"directory does not exist: {rel}")
    if not _is_writable_relative(f"{rel}/placeholder", settings):
        raise WritePolicyError(f"directory is not writable by policy: {rel}")
    return target, rel


def _check_expected_hash(old_sha: str | None, expected_sha256: str | None, settings: Settings, path: str) -> None:
    if old_sha is None and expected_sha256 is None:
        return
    if settings.require_expected_hash_for_writes and not expected_sha256:
        raise StaleWriteError(f"expected_sha256 is required for writes: {path}")
    if expected_sha256 and old_sha != expected_sha256:
        raise StaleWriteError(f"stale write rejected for {path}: expected {expected_sha256}, current {old_sha}")


def structured_error(exc: Exception) -> dict[str, Any]:
    message = str(exc)
    if isinstance(exc, StaleWriteError):
        return {"ok": False, "error_kind": "stale_expected_hash", "error": message}
    if isinstance(exc, SecurityError):
        return {"ok": False, "error_kind": "path_traversal_or_blocked", "error": message}
    if isinstance(exc, WritePolicyError):
        if "binary" in message or "UTF-8" in message:
            kind = "binary_or_non_utf8"
        elif "exceeds" in message:
            kind = "payload_too_large"
        elif "not writable" in message:
            kind = "path_not_writable"
        else:
            kind = "write_policy_error"
        return {"ok": False, "error_kind": kind, "error": message}
    if isinstance(exc, PatchApplyError):
        return {"ok": False, "error_kind": "patch_rejected", "error": message}
    if isinstance(exc, FileNotFoundError):
        return {"ok": False, "error_kind": "file_not_found", "error": message}
    if "anchor not found" in message or "heading not found" in message or "fragment not found" in message:
        return {"ok": False, "error_kind": "anchor_not_found", "error": message}
    return {"ok": False, "error_kind": "validation_error", "error": message}


def _build_result(path: str, old_text: str, new_text: str, *, dry_run: bool) -> dict[str, Any]:
    old_sha = sha256_text(old_text)
    new_sha = sha256_text(new_text)
    added, removed = _line_delta(old_text, new_text)
    return {
        "ok": True,
        "path": path,
        "changed": old_text != new_text,
        "dry_run": dry_run,
        "old_sha256": old_sha,
        "new_sha256": new_sha,
        "diff_unified": _unified_diff(path, old_text, new_text),
        "lines_added": added,
        "lines_removed": removed,
    }


def _path_result(
    *,
    path: str,
    changed: bool,
    dry_run: bool,
    diff_unified: str = "",
    old_sha256: str | None = None,
    new_sha256: str | None = None,
    lines_added: int = 0,
    lines_removed: int = 0,
) -> dict[str, Any]:
    return {
        "ok": True,
        "path": path,
        "changed": changed,
        "dry_run": dry_run,
        "old_sha256": old_sha256,
        "new_sha256": new_sha256,
        "diff_unified": diff_unified,
        "lines_added": lines_added,
        "lines_removed": lines_removed,
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


def create_text_file(
    path: str,
    content: str,
    settings: Settings,
    *,
    overwrite: bool = False,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings, create_if_missing=True)
    if target.exists() and not overwrite:
        raise FileExistsError(f"file already exists: {rel}")
    old_text = _read_existing_text(target, settings) if target.exists() else ""
    _validate_new_text(rel, content, settings)
    result = _build_result(rel, old_text, content, dry_run=dry_run)
    if result["changed"] and not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return result


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


def _replace_lines_text(old_text: str, start_line: int, end_line: int, replacement: str) -> str:
    lines = old_text.splitlines(keepends=True)
    if start_line < 1 or end_line < start_line or end_line > len(lines):
        raise ValueError("invalid line range")
    replacement_text = replacement
    if replacement_text and not replacement_text.endswith("\n"):
        replacement_text += "\n"
    return "".join(lines[: start_line - 1]) + replacement_text + "".join(lines[end_line:])


def replace_lines(
    path: str,
    start_line: int,
    end_line: int,
    replacement: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    new_text = _replace_lines_text(old_text, start_line, end_line, replacement)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def _insert_at_line_text(old_text: str, line: int, content: str, *, after: bool) -> str:
    lines = old_text.splitlines(keepends=True)
    if line < 1 or line > len(lines):
        raise ValueError("invalid line number")
    content_text = content
    if content_text and not content_text.endswith("\n"):
        content_text += "\n"
    index = line if after else line - 1
    return "".join(lines[:index]) + content_text + "".join(lines[index:])


def insert_before_line(
    path: str,
    line: int,
    content: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    new_text = _insert_at_line_text(old_text, line, content, after=False)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def insert_after_line(
    path: str,
    line: int,
    content: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    new_text = _insert_at_line_text(old_text, line, content, after=True)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def _heading_line(old_text: str, heading: str) -> int:
    wanted = heading.strip()
    for index, line in enumerate(old_text.splitlines(), start=1):
        if line.strip() == wanted:
            return index
    raise ValueError(f"heading not found: {heading}")


def insert_before_heading(
    path: str,
    heading: str,
    content: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    line = _heading_line(old_text, heading)
    new_text = _insert_at_line_text(old_text, line, content, after=False)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def insert_after_heading(
    path: str,
    heading: str,
    content: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    line = _heading_line(old_text, heading)
    new_text = _insert_at_line_text(old_text, line, content, after=True)
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def append_to_file(
    path: str,
    content: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    separator = "" if not old_text or old_text.endswith("\n") else "\n"
    new_text = old_text + separator + content
    if new_text and not new_text.endswith("\n"):
        new_text += "\n"
    return _apply_text_change(rel, settings, new_text, create_if_missing=False, expected_sha256=expected_sha256, dry_run=dry_run)


def update_current_mission(
    section_title: str,
    content: str,
    settings: Settings,
    *,
    position: str = "before_goal",
    dry_run: bool = True,
) -> dict[str, Any]:
    path = "missions/CURRENT.md"
    target, _ = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    block = f"## {section_title.strip()}\n\n{content.strip()}\n\n"
    if position != "before_goal":
        raise ValueError("position must be 'before_goal'")
    return insert_before_heading(
        path,
        "## Goal",
        block,
        settings,
        expected_sha256=sha256_text(old_text),
        dry_run=dry_run,
    )


def ensure_directory(path: str, settings: Settings, *, dry_run: bool = True) -> dict[str, Any]:
    target, rel = resolve_write_dir_path(path, settings)
    changed = not target.exists()
    if changed and not dry_run:
        target.mkdir(parents=True, exist_ok=True)
    return _path_result(path=rel, changed=changed, dry_run=dry_run)


def delete_path(
    path: str,
    settings: Settings,
    *,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    _ensure_move_delete_allowed(settings)
    target, rel = resolve_write_path(path, settings)
    old_text = _read_existing_text(target, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, rel)
    result = _build_result(rel, old_text, "", dry_run=dry_run)
    if not dry_run:
        target.unlink()
    return result


def move_path(
    source_path: str,
    destination_path: str,
    settings: Settings,
    *,
    overwrite: bool = False,
    expected_sha256: str | None = None,
    dry_run: bool = True,
) -> dict[str, Any]:
    _ensure_move_delete_allowed(settings)
    source, source_rel = resolve_write_path(source_path, settings)
    destination, destination_rel = resolve_write_path(destination_path, settings, create_if_missing=True)
    old_text = _read_existing_text(source, settings)
    _check_expected_hash(sha256_text(old_text), expected_sha256, settings, source_rel)
    if destination.exists() and not overwrite:
        raise FileExistsError(f"destination already exists: {destination_rel}")
    if destination.exists():
        _read_existing_text(destination, settings)
    diff = _unified_diff(source_rel, old_text, "") + "\n" + _unified_diff(destination_rel, "", old_text)
    result = _path_result(
        path=source_rel,
        changed=source_rel != destination_rel,
        dry_run=dry_run,
        diff_unified=diff.strip(),
        old_sha256=sha256_text(old_text),
        new_sha256=sha256_text(old_text),
        lines_added=len(old_text.splitlines()),
        lines_removed=len(old_text.splitlines()),
    )
    result["destination_path"] = destination_rel
    if result["changed"] and not dry_run:
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
    return result


def _run_operation(operation: dict[str, Any], settings: Settings, *, dry_run: bool) -> dict[str, Any]:
    op = operation.get("type") or operation.get("op")
    if op == "write":
        return write_text_file(
            operation["path"],
            operation["content"],
            settings,
            create_if_missing=operation.get("create_if_missing", False),
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "replace":
        return replace_text_in_file(
            operation["path"],
            operation["find"],
            operation["replace"],
            settings,
            replace_all=operation.get("replace_all", False),
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "insert":
        return insert_text_in_file(
            operation["path"],
            operation["anchor"],
            operation["position"],
            operation["content"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "delete_text":
        return delete_text_in_file(
            operation["path"],
            settings,
            find=operation.get("find"),
            start_line=operation.get("start_line"),
            end_line=operation.get("end_line"),
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "create_file":
        return create_text_file(
            operation["path"],
            operation["content"],
            settings,
            overwrite=operation.get("overwrite", False),
            dry_run=dry_run,
        )
    if op == "move":
        return move_path(
            operation["source_path"],
            operation["destination_path"],
            settings,
            overwrite=operation.get("overwrite", False),
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "delete_file":
        return delete_path(
            operation["path"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "ensure_directory":
        return ensure_directory(operation["path"], settings, dry_run=dry_run)
    if op == "replace_lines":
        return replace_lines(
            operation["path"],
            operation["start_line"],
            operation["end_line"],
            operation["replacement"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "insert_before_line":
        return insert_before_line(
            operation["path"],
            operation["line"],
            operation["content"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "insert_after_line":
        return insert_after_line(
            operation["path"],
            operation["line"],
            operation["content"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "insert_before_heading":
        return insert_before_heading(
            operation["path"],
            operation["heading"],
            operation["content"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "insert_after_heading":
        return insert_after_heading(
            operation["path"],
            operation["heading"],
            operation["content"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    if op == "append_to_file":
        return append_to_file(
            operation["path"],
            operation["content"],
            settings,
            expected_sha256=operation.get("expected_sha256"),
            dry_run=dry_run,
        )
    raise ValueError(f"unsupported batch operation: {op}")


def _snapshot_paths(operations: list[dict[str, Any]], settings: Settings) -> dict[str, str | None]:
    snapshot: dict[str, str | None] = {}
    for operation in operations:
        for key in ("path", "source_path", "destination_path"):
            value = operation.get(key)
            if not value:
                continue
            root = settings.project_root.resolve()
            target = (root / value).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            rel = rel_posix(root, target) if target.exists() else normalize_rel_path(value)
            if rel not in snapshot:
                snapshot[rel] = _read_optional_text(target, settings) if target.exists() and target.is_file() else None
    return snapshot


def _restore_snapshot(snapshot: dict[str, str | None], settings: Settings) -> None:
    root = settings.project_root.resolve()
    for rel, text in snapshot.items():
        target = (root / rel).resolve()
        if text is None:
            if target.exists() and target.is_file():
                target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _combined_diff(results: list[dict[str, Any]], settings: Settings) -> str:
    combined = "\n".join(result.get("diff_unified", "") for result in results if result.get("diff_unified"))
    if len(combined) > settings.max_combined_diff_chars:
        return combined[: settings.max_combined_diff_chars] + "\n...[truncated]"
    return combined


def batch_edit_files(
    operations: list[dict[str, Any]],
    settings: Settings,
    *,
    atomic: bool = True,
    dry_run: bool = True,
) -> dict[str, Any]:
    if len(operations) > settings.max_batch_operations:
        raise ValueError(f"too many operations; max is {settings.max_batch_operations}")
    results: list[dict[str, Any]] = []
    snapshot = _snapshot_paths(operations, settings) if atomic and not dry_run else {}
    rollback_performed = False
    failed_index: int | None = None

    for index, operation in enumerate(operations):
        try:
            result = _run_operation(operation, settings, dry_run=dry_run)
            result["operation_index"] = index
            result["op"] = operation.get("type") or operation.get("op")
            result["ok"] = True
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            failed_index = index
            error = structured_error(exc)
            error.update({"operation_index": index, "op": operation.get("type") or operation.get("op")})
            results.append(error)
            if atomic and not dry_run:
                _restore_snapshot(snapshot, settings)
                rollback_performed = True
            if atomic:
                break

    return {
        "operations_total": len(operations),
        "operations_applied": sum(1 for item in results if item.get("ok") and item.get("changed")),
        "atomic": atomic,
        "dry_run": dry_run,
        "results": results,
        "combined_diff": _combined_diff(results, settings),
        "failed_operation_index": failed_index,
        "rollback_performed": rollback_performed,
    }


def _extract_patch_paths(patch: str) -> list[str]:
    paths: list[str] = []
    for line in patch.splitlines():
        match = re.match(r"^(?:---|\+\+\+) [ab]/(.+)$", line)
        if not match:
            continue
        rel = normalize_rel_path(match.group(1))
        if rel != "/dev/null" and rel not in paths:
            paths.append(rel)
    return paths


def _run_git_apply(settings: Settings, patch: str, *, check: bool) -> subprocess.CompletedProcess[str]:
    args = ["git", "apply"]
    if check:
        args.append("--check")
    return subprocess.run(
        args,
        cwd=str(settings.project_root),
        input=patch,
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )


def _git_head(settings: Settings) -> str:
    proc = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(settings.project_root),
        text=True,
        capture_output=True,
        check=False,
        timeout=settings.subprocess_timeout,
    )
    if proc.returncode != 0:
        raise PatchApplyError(proc.stderr.strip() or "git rev-parse HEAD failed")
    return proc.stdout.strip()


def apply_patch_diff(
    patch: str,
    settings: Settings,
    *,
    dry_run: bool = True,
    expected_base_sha: str | None = None,
) -> dict[str, Any]:
    if len(patch.encode("utf-8")) > settings.max_patch_bytes:
        raise WritePolicyError("patch exceeds MAX_PATCH_BYTES")
    base_sha = _git_head(settings)
    if expected_base_sha and expected_base_sha != base_sha:
        raise StaleWriteError(f"stale base sha: expected {expected_base_sha}, current {base_sha}")
    changed_files = _extract_patch_paths(patch)
    if not changed_files:
        raise PatchApplyError("patch does not contain git-style file paths")
    for path in changed_files:
        resolve_write_path(path, settings, create_if_missing=True)
    check = _run_git_apply(settings, patch, check=True)
    if check.returncode != 0:
        raise PatchApplyError(check.stderr.strip() or check.stdout.strip() or "git apply --check failed")
    if not dry_run:
        apply = _run_git_apply(settings, patch, check=False)
        if apply.returncode != 0:
            raise PatchApplyError(apply.stderr.strip() or apply.stdout.strip() or "git apply failed")
    return {
        "ok": True,
        "changed": bool(changed_files),
        "dry_run": dry_run,
        "applied": not dry_run,
        "base_sha": base_sha,
        "changed_files": changed_files,
        "diff_unified": patch,
    }
