from __future__ import annotations

import json
import os
import re
import tomllib
from pathlib import Path
from typing import Any

from .config import Settings
from .security import rel_posix, resolve_repo_path


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


def list_dir(path: str, settings: Settings, include_hidden: bool = False, limit: int = 200) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=include_hidden)
    if not target.is_dir():
        raise ValueError(f"not a directory: {path}")

    entries = []
    root = settings.project_root.resolve()
    for entry in sorted(target.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        rel = rel_posix(root, entry)
        if not include_hidden and any(part.startswith(".") for part in Path(rel).parts):
            continue
        if len(entries) >= min(limit, settings.max_tree_entries):
            break
        entries.append(
            {
                "name": entry.name,
                "path": rel,
                "type": "dir" if entry.is_dir() else "file",
                "size": entry.stat().st_size if entry.is_file() else None,
            }
        )
    return {"path": rel_posix(root, target), "entries": entries, "truncated": len(entries) >= limit}


def tree(path: str, settings: Settings, depth: int = 4, include_hidden: bool = False) -> dict[str, Any]:
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
            rel = rel_posix(root, child)
            if not include_hidden and any(part.startswith(".") for part in Path(rel).parts):
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
    target = resolve_repo_path(path, settings)
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
    include_hidden: bool = False,
    limit: int = 200,
) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=include_hidden)
    if not target.is_dir():
        raise ValueError(f"not a directory: {path}")

    limit = min(limit, settings.max_tree_entries)
    root = settings.project_root.resolve()
    matches: list[str] = []
    for found in target.rglob(pattern):
        rel = rel_posix(root, found)
        if not include_hidden and any(part.startswith(".") for part in Path(rel).parts):
            continue
        matches.append(rel)
        if len(matches) >= limit:
            break
    return {"pattern": pattern, "path": rel_posix(root, target), "matches": matches, "count": len(matches)}


def search_text(
    query: str,
    settings: Settings,
    path: str = ".",
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=settings.allow_hidden_default)
    limit = min(limit, settings.max_search_results)

    flags = 0 if case_sensitive else re.IGNORECASE
    compiled = re.compile(query if regex else re.escape(query), flags)

    results: list[dict[str, Any]] = []
    root = settings.project_root.resolve()

    if target.is_file():
        candidates = [target]
    else:
        candidates = [p for p in target.rglob("*") if p.is_file()]

    for file_path in candidates:
        rel = rel_posix(root, file_path)
        if any(part.startswith(".") for part in Path(rel).parts) and not settings.allow_hidden_default:
            continue
        try:
            text = _read_text(file_path, settings)
        except Exception:
            continue
        for idx, line in enumerate(text.splitlines(), start=1):
            if compiled.search(line):
                results.append({"path": rel, "line": idx, "text": _truncate(line, 400)})
                if len(results) >= limit:
                    return {"query": query, "regex": regex, "results": results, "count": len(results)}
    return {"query": query, "regex": regex, "results": results, "count": len(results)}


def symbol_search(symbol: str, settings: Settings, path: str = ".", limit: int = 100) -> dict[str, Any]:
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
        batch = search_text(pattern, settings, path=path, regex=True, case_sensitive=True, limit=limit)
        for item in batch["results"]:
            if item not in results:
                results.append(item)
            if len(results) >= limit:
                return {"symbol": symbol, "results": results, "count": len(results)}
    return {"symbol": symbol, "results": results, "count": len(results)}


def recent_changes(settings: Settings, path: str = ".", limit: int = 100) -> dict[str, Any]:
    target = resolve_repo_path(path, settings, allow_hidden=settings.allow_hidden_default)
    root = settings.project_root.resolve()
    items = []

    if target.is_file():
        candidates = [target]
    else:
        candidates = [p for p in target.rglob("*") if p.is_file()]

    for file_path in candidates:
        rel = rel_posix(root, file_path)
        if any(part.startswith(".") for part in Path(rel).parts) and not settings.allow_hidden_default:
            continue
        stat = file_path.stat()
        items.append({"path": rel, "mtime": stat.st_mtime, "size": stat.st_size})

    items.sort(key=lambda x: x["mtime"], reverse=True)
    items = items[:limit]
    return {"path": rel_posix(root, target), "files": items, "count": len(items)}


def todo_scan(settings: Settings, path: str = ".", limit: int = 100) -> dict[str, Any]:
    return search_text(r"\b(TODO|FIXME|XXX|HACK)\b", settings, path=path, regex=True, limit=limit)


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
        manifests = [target]
    else:
        names = ["pyproject.toml", "requirements.txt", "package.json", "go.mod", "Cargo.toml"]
        for name in names:
            manifests.extend(target.rglob(name))

    parsed: dict[str, Any] = {}
    for manifest in manifests:
        rel = rel_posix(root, manifest)
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
