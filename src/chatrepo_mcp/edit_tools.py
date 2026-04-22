from __future__ import annotations

import difflib
import hashlib
import shutil
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
    op = operation.get("op")
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
            result["op"] = operation.get("op")
            result["ok"] = True
            results.append(result)
        except Exception as exc:  # noqa: BLE001
            failed_index = index
            results.append({"operation_index": index, "op": operation.get("op"), "ok": False, "error": str(exc)})
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
