from __future__ import annotations

import json
import os
import re
import subprocess
import tomllib
from hashlib import sha256
from fnmatch import fnmatch
from pathlib import Path
from typing import Any

from .config import Settings
from .security import is_allowed_relative, is_blocked_relative, rel_posix, rel_posix_lexical, resolve_repo_path


TEXT_EXTENSIONS = {
    ".py", ".pyi", ".js", ".ts", ".tsx", ".jsx", ".java", ".kt", ".go", ".rs", ".c",
    ".h", ".hpp", ".cpp", ".cs", ".rb", ".php", ".swift", ".scala", ".lua", ".sh", ".zsh",
    ".bash", ".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".sql",
    ".xml", ".html", ".css", ".scss", ".dockerfile", ".env.example",
}


def _is_probably_text(path: Path) -> bool:
    if path.suffix.lower() in TEXT_EXTENSIONS:
        return True
    if path.name in {"Dockerfile", "Makefile", "README", "README.md", "requirements.txt", "go.mod"}:
        return True
    return True


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...[truncated]"


def _read_text(path: Path, settings: Settings) -> str:
    data = path.read_bytes()
    if len(data) > settings.max_file_bytes:
        raise ValueError(
            f"file exceeds MAX_FILE_BYTES ({len(data)} > {settings.max_file_bytes}): {path.name}"
        )
    return data.decode("utf-8", errors="replace")


def _safe_rel(root: Path, path: Path) -> str | None:
    try:
        return rel_posix_lexical(root, path)
    except ValueError:
        return None


def _entry_allowed(root: Path, path: Path, settings: Settings, *, allow_hidden: bool) -> tuple[bool, str | None]:
    rel = _safe_rel(root, path)
    if rel is None:
        return False, None
    if not is_allowed_relative(rel, settings, allow_hidden=allow_hidden):
        return False, rel
    if path.is_symlink():
        try:
            resolved_rel = rel_posix(root, path)
        except ValueError:
            return False, rel
        if not is_allowed_relative(resolved_rel, settings, allow_hidden=allow_hidden):
            return False, rel
    return True, rel


def _iter_files(root: Path, target: Path, settings: Settings, *, allow_hidden: bool) -> list[Path]:
    files: list[Path] = []
    if target.is_file():
        ok, _ = _entry_allowed(root, target, settings, allow_hidden=allow_hidden)
        return [target] if ok else []

    for current, dirnames, filenames in os.walk(target, followlinks=False):
        current_path = Path(current)
        kept_dirs = []
        for dirname in dirnames:
            child = current_path / dirname
            ok, _ = _entry_allowed(root, child, settings, allow_hidden=allow_hidden)
            if ok:
                kept_dirs.append(dirname)
        dirnames[:] = kept_dirs
        for filename in filenames:
            child = current_path / filename
            ok, _ = _entry_allowed(root, child, settings, allow_hidden=allow_hidden)
            if ok:
                files.append(child)
    return files


def _rg_exclude_globs(settings: Settings) -> list[str]:
    globs: list[str] = []
    for pattern in settings.blocked_globs:
        globs.extend(["--glob", f"!{pattern}"])
        if pattern.startswith("**/") and pattern.endswith("/**"):
            name = pattern[3:-3]
            globs.extend(["--glob", f"!{name}", "--glob", f"!**/{name}/**"])
        elif "/" not in pattern:
            globs.extend(["--glob", f"!**/{pattern}"])
    return globs


def repo_info(settings: Settings) -> dict[str, Any]:
    root = settings.project_root.resolve()
    return {
        "project_root": str(root),
        "exists": root.exists(),
        "is_dir": root.is_dir(),
        "config": {
            "transport": settings.transport,
            "max_file_bytes": settings.max_file_bytes,
            "max_response_chars": settings.max_response_chars,
            "max_read_files": settings.max_read_files,
            "max_search_results": settings.max_search_results,
            "max_tree_entries": settings.max_tree_entries,
            "blocked_globs": list(settings.blocked_globs),
        },
    }


def list_dir(path: str, settings: Settings, include_hidden: bool = True, limit: int = 200) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=include_hidden)
    if not target.is_dir():
        raise ValueError(f"not a directory: {path}")

    entries = []
    root = settings.project_root.resolve()
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        ok, rel = _entry_allowed(root, entry, settings, allow_hidden=include_hidden)
        if not ok or rel is None:
            continue
        if len(entries) >= min(limit, settings.max_tree_entries):
            break
        stat = entry.lstat()
        entries.append(
            {
                "name": entry.name,
                "path": rel,
                "type": "symlink" if entry.is_symlink() else "dir" if entry.is_dir() else "file",
                "size": stat.st_size if entry.is_file() or entry.is_symlink() else None,
            }
        )
    return {"path": rel_posix(root, target), "entries": entries, "truncated": len(entries) >= limit}


def tree(path: str, settings: Settings, depth: int = 4, include_hidden: bool = True) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=include_hidden)
    if not target.is_dir():
        raise ValueError(f"not a directory: {path}")

    root = settings.project_root.resolve()
    lines: list[str] = []
    count = 0
    max_entries = settings.max_tree_entries

    def walk(node: Path, prefix: str, remaining_depth: int) -> None:
        nonlocal count
        if count >= max_entries or remaining_depth < 0:
            return

        children = []
        for child in sorted(node.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
            ok, _ = _entry_allowed(root, child, settings, allow_hidden=include_hidden)
            if not ok:
                continue
            children.append(child)

        for idx, child in enumerate(children):
            if count >= max_entries:
                return
            connector = "└── " if idx == len(children) - 1 else "├── "
            lines.append(f"{prefix}{connector}{child.name}/" if child.is_dir() else f"{prefix}{connector}{child.name}")
            count += 1
            if child.is_dir() and remaining_depth > 0:
                extension = "    " if idx == len(children) - 1 else "│   "
                walk(child, prefix + extension, remaining_depth - 1)

    lines.append(rel_posix(root, target) if target != root else ".")
    walk(target, "", depth)
    return {"path": rel_posix(root, target), "tree": "\n".join(lines), "entries": count, "max_entries": max_entries}


def read_text_file(
    path: str,
    settings: Settings,
    start_line: int = 1,
    end_line: int | None = None,
    with_line_numbers: bool = True,
) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=settings.allow_hidden_default)
    if not target.is_file():
        raise ValueError(f"not a file: {path}")
    if not _is_probably_text(target):
        raise ValueError(f"unsupported or binary file: {path}")

    text = _read_text(target, settings)
    lines = text.splitlines()

    if start_line < 1:
        start_line = 1
    if end_line is None or end_line > len(lines):
        end_line = len(lines)
    if end_line < start_line:
        raise ValueError("end_line must be >= start_line")

    selected = lines[start_line - 1 : end_line]
    if with_line_numbers:
        rendered = "\n".join(f"{i}: {line}" for i, line in enumerate(selected, start=start_line))
    else:
        rendered = "\n".join(selected)

    rendered = _truncate(rendered, settings.max_response_chars)
    return {
        "path": rel_posix(settings.project_root, target),
        "start_line": start_line,
        "end_line": end_line,
        "content": rendered,
        "line_count": len(lines),
        "sha256": sha256(text.encode("utf-8")).hexdigest(),
    }


def read_multiple_files(paths: list[str], settings: Settings) -> dict[str, Any]:
    if not paths:
        raise ValueError("paths must not be empty")
    if len(paths) > settings.max_read_files:
        raise ValueError(f"too many paths; max is {settings.max_read_files}")

    results = []
    for item in paths:
        try:
            results.append(read_text_file(item, settings))
        except Exception as exc:  # noqa: BLE001
            results.append({"path": item, "error": str(exc)})
    return {"files": results}


def file_metadata(path: str, settings: Settings, include_stat: bool = True) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=settings.allow_hidden_default)
    st = target.stat()
    result = {
        "path": rel_posix(settings.project_root, target),
        "exists": target.exists(),
        "type": "dir" if target.is_dir() else "file",
        "name": target.name,
        "suffix": target.suffix,
    }
    if include_stat:
        result.update(
            {
                "size": st.st_size,
                "mtime": st.st_mtime,
                "ctime": st.st_ctime,
                "mode": oct(st.st_mode & 0o777),
            }
        )
    return result


def find_files(
    pattern: str,
    settings: Settings,
    path: str = ".",
    include_hidden: bool = True,
    limit: int = 200,
) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=include_hidden)
    if not target.is_dir():
        raise ValueError(f"not a directory: {path}")

    limit = min(limit, settings.max_tree_entries)
    root = settings.project_root.resolve()
    matches: list[str] = []
    for found in _iter_files(root, target, settings, allow_hidden=include_hidden):
        rel = rel_posix_lexical(root, found)
        if fnmatch(found.name, pattern) or fnmatch(rel, pattern) or found.match(pattern):
            matches.append(rel)
        if len(matches) >= limit:
            break
    return {"pattern": pattern, "path": rel_posix(root, target), "matches": matches, "count": len(matches)}


def search_text(
    query: str,
    settings: Settings,
    path: str = ".",
    paths: list[str] | None = None,
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    limit = min(limit, settings.max_search_results)
    results: list[dict[str, Any]] = []
    root = settings.project_root.resolve()
    search_paths = paths if paths else [path]
    rel_paths = []

    for item in search_paths:
        target = resolve_repo_path(item, settings, allow_hidden=settings.allow_hidden_default)
        ok, rel_path = _entry_allowed(root, target, settings, allow_hidden=settings.allow_hidden_default)
        if ok and rel_path is not None:
            rel_paths.append(rel_path)
    if not rel_paths:
        return {"query": query, "regex": regex, "path": path, "paths": search_paths, "results": [], "count": 0}

    cmd = [
        "rg",
        "--hidden",
        "-nI",
        "--with-filename",
        "--no-heading",
        "--color",
        "never",
        "--max-count",
        str(max(limit, 1)),
    ]
    if not regex:
        cmd.append("--fixed-strings")
    if not case_sensitive:
        cmd.append("--ignore-case")
    cmd.extend(_rg_exclude_globs(settings))
    cmd.extend(["--", query, *rel_paths])

    proc = subprocess.run(
        cmd,
        cwd=str(root),
        check=False,
        capture_output=True,
        text=True,
        timeout=settings.subprocess_timeout,
    )
    if proc.returncode not in {0, 1}:
        stderr = proc.stderr.strip() or proc.stdout.strip()
        raise RuntimeError(stderr or "ripgrep search failed")

    for line in proc.stdout.splitlines():
        file_path, line_no, text = line.split(":", 2) if line.count(":") >= 2 else (None, None, None)
        if file_path is None or line_no is None or text is None:
            continue
        if file_path.startswith("./"):
            file_path = file_path[2:]
        if not line_no.isdigit():
            continue
        if not is_allowed_relative(file_path, settings, allow_hidden=settings.allow_hidden_default):
            continue
        results.append({"path": file_path, "line": int(line_no), "text": _truncate(text, 400)})
        if len(results) >= limit:
            break
    return {"query": query, "regex": regex, "path": path, "paths": search_paths, "results": results, "count": len(results)}


def symbol_search(
    symbol: str,
    settings: Settings,
    path: str = ".",
    paths: list[str] | None = None,
    limit: int = 100,
) -> dict[str, Any]:
    patterns = [
        rf"\bdef\s+{re.escape(symbol)}\b",
        rf"\bclass\s+{re.escape(symbol)}\b",
        rf"\bfunction\s+{re.escape(symbol)}\b",
        rf"\bconst\s+{re.escape(symbol)}\b",
        rf"\blet\s+{re.escape(symbol)}\b",
        rf"\bvar\s+{re.escape(symbol)}\b",
        rf"\binterface\s+{re.escape(symbol)}\b",
        rf"\btype\s+{re.escape(symbol)}\b",
        rf"\bstruct\s+{re.escape(symbol)}\b",
    ]
    results: list[dict[str, Any]] = []
    for pattern in patterns:
        batch = search_text(pattern, settings, path=path, paths=paths, regex=True, case_sensitive=True, limit=limit)
        for item in batch["results"]:
            if item not in results:
                results.append(item)
            if len(results) >= limit:
                return {"symbol": symbol, "results": results, "count": len(results)}
    if not results:
        batch = search_text(symbol, settings, path=path, paths=paths, regex=False, case_sensitive=True, limit=limit)
        results.extend(batch["results"])
    return {"symbol": symbol, "results": results[:limit], "count": len(results[:limit])}


def recent_changes(settings: Settings, path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict[str, Any]:
    root = settings.project_root.resolve()
    items = []
    search_paths = paths if paths else [path]

    for item in search_paths:
        target = resolve_repo_path(item, settings, allow_hidden=settings.allow_hidden_default)
        for file_path in _iter_files(root, target, settings, allow_hidden=settings.allow_hidden_default):
            rel = rel_posix_lexical(root, file_path)
            stat = file_path.stat()
            row = {"path": rel, "mtime": stat.st_mtime, "size": stat.st_size}
            if row not in items:
                items.append(row)

    items.sort(key=lambda x: x["mtime"], reverse=True)
    items = items[:limit]
    return {"path": path, "paths": search_paths, "files": items, "count": len(items)}


def todo_scan(settings: Settings, path: str = ".", paths: list[str] | None = None, limit: int = 100) -> dict[str, Any]:
    return search_text(r"\b(TODO|FIXME|XXX|HACK)\b", settings, path=path, paths=paths, regex=True, limit=limit)


def _parse_requirements_txt(path: Path) -> list[str]:
    deps = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-r"):
            continue
        deps.append(line)
    return deps


def _parse_pyproject(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    result = {}
    project = data.get("project", {})
    poetry = data.get("tool", {}).get("poetry", {})
    if project:
        result["project.dependencies"] = project.get("dependencies", [])
        result["project.optional-dependencies"] = project.get("optional-dependencies", {})
    if poetry:
        result["tool.poetry.dependencies"] = poetry.get("dependencies", {})
        result["tool.poetry.group"] = poetry.get("group", {})
    return result


def _parse_package_json(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    keys = [
        "name",
        "version",
        "dependencies",
        "devDependencies",
        "peerDependencies",
        "optionalDependencies",
    ]
    return {k: data.get(k) for k in keys if k in data}


def _parse_go_mod(path: Path) -> list[str]:
    deps = []
    in_block = False
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if s.startswith("require ("):
            in_block = True
            continue
        if in_block and s == ")":
            in_block = False
            continue
        if in_block and s and not s.startswith("//"):
            deps.append(s)
        elif s.startswith("require "):
            deps.append(s.removeprefix("require ").strip())
    return deps


def _parse_cargo_toml(path: Path) -> dict[str, Any]:
    data = tomllib.loads(path.read_text(encoding="utf-8", errors="replace"))
    return {
        "package": data.get("package", {}),
        "dependencies": data.get("dependencies", {}),
        "dev-dependencies": data.get("dev-dependencies", {}),
    }


def dependency_map(settings: Settings, path: str = ".") -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=settings.allow_hidden_default)
    root = settings.project_root.resolve()

    manifests = []
    if target.is_file():
        manifests = _iter_files(root, target, settings, allow_hidden=settings.allow_hidden_default)
    else:
        names = ["pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml"]
        for file_path in _iter_files(root, target, settings, allow_hidden=settings.allow_hidden_default):
            if file_path.name in names:
                manifests.append(file_path)

    parsed: dict[str, Any] = {}
    for manifest in manifests:
        rel = rel_posix_lexical(root, manifest)
        if is_blocked_relative(rel, settings):
            continue
        try:
            if manifest.name == "pyproject.toml":
                parsed[rel] = _parse_pyproject(manifest)
            elif manifest.name == "requirements.txt":
                parsed[rel] = _parse_requirements_txt(manifest)
            elif manifest.name == "package.json":
                parsed[rel] = _parse_package_json(manifest)
            elif manifest.name == "go.mod":
                parsed[rel] = _parse_go_mod(manifest)
            elif manifest.name == "Cargo.toml":
                parsed[rel] = _parse_cargo_toml(manifest)
        except Exception as exc:  # noqa: BLE001
            parsed[rel] = {"error": str(exc)}
    return {"manifests": parsed, "count": len(parsed)}
