from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .config import Settings
from .fs_tools import (
    dependency_map,
    file_metadata,
    find_files,
    list_dir,
    read_multiple_files,
    read_text_file,
    recent_changes,
    repo_info,
    search_text,
    symbol_search,
    todo_scan,
    tree,
)
from .git_tools import (
    git_blame,
    git_branches,
    git_diff,
    git_grep,
    git_log,
    git_show,
    git_status,
    repo_git_info,
)

settings = Settings.from_env()

mcp = FastMCP(
    settings.app_name,
    host=settings.host,
    port=settings.port,
    streamable_http_path="/mcp",
    json_response=True,
    transport_security=TransportSecuritySettings(
        enable_dns_rebinding_protection=settings.enable_dns_rebinding_protection,
        allowed_hosts=list(settings.allowed_hosts),
        allowed_origins=[],
    ),
)

READ_ONLY = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "openWorldHint": False,
}


@mcp.tool(
    name="repo_info",
    annotations={**READ_ONLY, "title": "Repository Info"},
)
def repo_info_tool() -> dict:
    """Return the MCP server configuration relevant to the inspected repository."""
    return _repo_info_with_git()


def _repo_info_with_git() -> dict:
    result = repo_info(settings)
    try:
        result["git"] = repo_git_info(settings)
    except Exception as exc:  # noqa: BLE001
        result["git_error"] = str(exc)
    return result


@mcp.tool(
    name="list_dir",
    annotations={**READ_ONLY, "title": "List Directory"},
)
def list_dir_tool(path: str = ".", include_hidden: bool = True, limit: int = 200) -> dict:
    """List files and directories under a repo-relative path."""
    return list_dir(path=path, settings=settings, include_hidden=include_hidden, limit=limit)


@mcp.tool(
    name="tree",
    annotations={**READ_ONLY, "title": "Tree"},
)
def tree_tool(path: str = ".", depth: int = 4, include_hidden: bool = True) -> dict:
    """Return a textual directory tree for a repo-relative path."""
    return tree(path=path, settings=settings, depth=depth, include_hidden=include_hidden)


@mcp.tool(
    name="read_text_file",
    annotations={**READ_ONLY, "title": "Read Text File"},
)
def read_text_file_tool(
    path: str,
    start_line: int = 1,
    end_line: int | None = None,
    with_line_numbers: bool = True,
) -> dict:
    """Read a text file from the repository, optionally limiting the line range."""
    return read_text_file(
        path=path,
        settings=settings,
        start_line=start_line,
        end_line=end_line,
        with_line_numbers=with_line_numbers,
    )


@mcp.tool(
    name="read_multiple_files",
    annotations={**READ_ONLY, "title": "Read Multiple Files"},
)
def read_multiple_files_tool(paths: list[str]) -> dict:
    """Read several repo files at once. Useful when comparing modules or gathering context."""
    return read_multiple_files(paths=paths, settings=settings)


@mcp.tool(
    name="file_metadata",
    annotations={**READ_ONLY, "title": "File Metadata"},
)
def file_metadata_tool(path: str, include_stat: bool = True) -> dict:
    """Return basic metadata for a repo-relative file or directory."""
    return file_metadata(path=path, settings=settings, include_stat=include_stat)


@mcp.tool(
    name="find_files",
    annotations={**READ_ONLY, "title": "Find Files"},
)
def find_files_tool(
    pattern: str,
    path: str = ".",
    include_hidden: bool = True,
    limit: int = 200,
) -> dict:
    """Find files by glob pattern below a repo-relative path."""
    return find_files(
        pattern=pattern,
        settings=settings,
        path=path,
        include_hidden=include_hidden,
        limit=limit,
    )


@mcp.tool(
    name="search_text",
    annotations={**READ_ONLY, "title": "Search Text"},
)
def search_text_tool(
    query: str,
    path: str = ".",
    regex: bool = False,
    case_sensitive: bool = False,
    limit: int = 100,
) -> dict:
    """Search text across the repository or within a specific file/directory."""
    return search_text(
        query=query,
        settings=settings,
        path=path,
        regex=regex,
        case_sensitive=case_sensitive,
        limit=limit,
    )


@mcp.tool(
    name="symbol_search",
    annotations={**READ_ONLY, "title": "Symbol Search"},
)
def symbol_search_tool(symbol: str, path: str = ".", limit: int = 100) -> dict:
    """Heuristically search for declarations or references of a symbol name."""
    return symbol_search(symbol=symbol, settings=settings, path=path, limit=limit)


@mcp.tool(
    name="recent_changes",
    annotations={**READ_ONLY, "title": "Recent Changes"},
)
def recent_changes_tool(path: str = ".", limit: int = 100) -> dict:
    """Return files sorted by recent filesystem modification time."""
    return recent_changes(settings=settings, path=path, limit=limit)


@mcp.tool(
    name="todo_scan",
    annotations={**READ_ONLY, "title": "Todo Scan"},
)
def todo_scan_tool(path: str = ".", limit: int = 100) -> dict:
    """Find TODO, FIXME, XXX, and HACK markers across the repository."""
    return todo_scan(settings=settings, path=path, limit=limit)


@mcp.tool(
    name="dependency_map",
    annotations={**READ_ONLY, "title": "Dependency Map"},
)
def dependency_map_tool(path: str = ".") -> dict:
    """Parse common dependency manifest files such as pyproject.toml, package.json, go.mod, and Cargo.toml."""
    return dependency_map(settings=settings, path=path)


@mcp.tool(
    name="git_status",
    annotations={**READ_ONLY, "title": "Git Status"},
)
def git_status_tool(short: bool = True) -> dict:
    """Return the current git status for the repository."""
    return git_status(settings=settings, short=short)


@mcp.tool(
    name="git_diff",
    annotations={**READ_ONLY, "title": "Git Diff"},
)
def git_diff_tool(
    staged: bool = False,
    pathspec: str | None = None,
    context_lines: int = 3,
) -> dict:
    """Return git diff output for the working tree or staged changes."""
    return git_diff(settings=settings, staged=staged, pathspec=pathspec, context_lines=context_lines)


@mcp.tool(
    name="git_log",
    annotations={**READ_ONLY, "title": "Git Log"},
)
def git_log_tool(limit: int = 20, pathspec: str | None = None, since: str | None = None) -> dict:
    """Return recent commit history, optionally filtered by path or since-date."""
    return git_log(settings=settings, limit=limit, pathspec=pathspec, since=since)


@mcp.tool(
    name="git_show",
    annotations={**READ_ONLY, "title": "Git Show"},
)
def git_show_tool(revision: str, path: str | None = None) -> dict:
    """Show a commit object or a file at a given revision."""
    return git_show(settings=settings, revision=revision, path=path)


@mcp.tool(
    name="git_branches",
    annotations={**READ_ONLY, "title": "Git Branches"},
)
def git_branches_tool(all_branches: bool = True) -> dict:
    """List local or all branches with tracking information."""
    return git_branches(settings=settings, all_branches=all_branches)


@mcp.tool(
    name="git_blame",
    annotations={**READ_ONLY, "title": "Git Blame"},
)
def git_blame_tool(path: str, start_line: int = 1, end_line: int | None = None) -> dict:
    """Blame a file line range to see who changed it and in which commit."""
    return git_blame(settings=settings, path=path, start_line=start_line, end_line=end_line)


@mcp.tool(
    name="git_grep",
    annotations={**READ_ONLY, "title": "Git Grep"},
)
def git_grep_tool(
    query: str,
    revision: str | None = None,
    pathspec: str | None = None,
    case_sensitive: bool = False,
) -> dict:
    """Search tracked content through git grep, optionally at a revision."""
    return git_grep(
        settings=settings,
        query=query,
        revision=revision,
        pathspec=pathspec,
        case_sensitive=case_sensitive,
    )


TOOL_NAMES = [
    "dependency_map",
    "file_metadata",
    "find_files",
    "git_blame",
    "git_branches",
    "git_diff",
    "git_grep",
    "git_log",
    "git_show",
    "git_status",
    "list_dir",
    "read_multiple_files",
    "read_text_file",
    "recent_changes",
    "repo_info",
    "search_text",
    "symbol_search",
    "todo_scan",
    "tree",
    "doctor",
    "context_bootstrap",
    "batch_call",
    "smoke_all",
]


def _namespace_info() -> dict:
    return {
        "canonical_namespace": settings.canonical_namespace,
        "canonical_tool_prefix": f"{settings.canonical_namespace}/",
        "ephemeral_handles_supported": settings.ephemeral_handles_supported,
        "ephemeral_handles_note": "Use the canonical namespace for stable calls; link_* handles are session-scoped and should not be treated as durable.",
    }


def _batch_dispatch(tool: str, args: dict | None = None) -> dict:
    args = args or {}
    handlers = {
        "repo_info": lambda: _repo_info_with_git(),
        "list_dir": lambda: list_dir(settings=settings, **args),
        "tree": lambda: tree(settings=settings, **args),
        "read_text_file": lambda: read_text_file(settings=settings, **args),
        "read_multiple_files": lambda: read_multiple_files(settings=settings, **args),
        "file_metadata": lambda: file_metadata(settings=settings, **args),
        "find_files": lambda: find_files(settings=settings, **args),
        "search_text": lambda: search_text(settings=settings, **args),
        "symbol_search": lambda: symbol_search(settings=settings, **args),
        "recent_changes": lambda: recent_changes(settings=settings, **args),
        "todo_scan": lambda: todo_scan(settings=settings, **args),
        "dependency_map": lambda: dependency_map(settings=settings, **args),
        "git_status": lambda: git_status(settings=settings, **args),
        "git_diff": lambda: git_diff(settings=settings, **args),
        "git_log": lambda: git_log(settings=settings, **args),
        "git_show": lambda: git_show(settings=settings, **args),
        "git_branches": lambda: git_branches(settings=settings, **args),
        "git_blame": lambda: git_blame(settings=settings, **args),
        "git_grep": lambda: git_grep(settings=settings, **args),
    }
    if tool not in handlers:
        raise ValueError(f"tool is not allowed for batch_call: {tool}")
    return handlers[tool]()


@mcp.tool(
    name="doctor",
    annotations={**READ_ONLY, "title": "Doctor"},
)
def doctor_tool() -> dict:
    """Run a compact health check for repository, git, policy, search, and symbol tools."""
    checks = {}
    try:
        checks["repo_info"] = {"ok": True, "result": _repo_info_with_git()}
    except Exception as exc:  # noqa: BLE001
        checks["repo_info"] = {"ok": False, "error": str(exc)}
    for name, args in [
        ("git_status", {"short": True}),
        ("read_text_file", {"path": ".claude/MEMORY.md", "start_line": 1, "end_line": 1}),
        ("read_text_file", {"path": ".env", "start_line": 1, "end_line": 1}),
        ("search_text", {"query": "tts_synthesizing", "path": ".", "limit": 1}),
        ("symbol_search", {"symbol": "tts_synthesizing", "path": ".", "limit": 1}),
    ]:
        key = "blocked_policy" if name == "read_text_file" and args["path"] == ".env" else name
        try:
            result = _batch_dispatch(name, args)
            checks[key] = {"ok": key != "blocked_policy", "result": result}
        except Exception as exc:  # noqa: BLE001
            checks[key] = {"ok": key == "blocked_policy", "error": str(exc)}
    return {
        "project_root": str(settings.project_root),
        **_namespace_info(),
        "tools": TOOL_NAMES,
        "tool_count": len(TOOL_NAMES),
        "checks": checks,
    }


@mcp.tool(
    name="smoke_all",
    annotations={**READ_ONLY, "title": "Smoke All"},
)
def smoke_all_tool() -> dict:
    """Run the standard Eva_Ai MCP smoke test in one call."""
    checks = []
    for name, args in [
        ("repo_info", {}),
        ("read_text_file", {"path": ".claude/MEMORY.md", "start_line": 1, "end_line": 1}),
        ("list_dir", {"path": ".", "limit": 300}),
        ("search_text", {"query": "tts_synthesizing", "path": ".", "limit": 3}),
        ("symbol_search", {"symbol": "tts_synthesizing", "path": ".", "limit": 3}),
        ("git_status", {"short": True}),
        ("git_log", {"limit": 3}),
        ("git_show", {"revision": "HEAD"}),
        ("read_text_file", {"path": ".env", "start_line": 1, "end_line": 1}),
    ]:
        key = "blocked_policy" if name == "read_text_file" and args["path"] == ".env" else name
        try:
            result = _batch_dispatch(name, args)
            ok = key != "blocked_policy"
            item = {"name": key, "tool": name, "ok": ok}
            if name == "list_dir":
                names = [entry["name"] for entry in result.get("entries", [])]
                item["blocked_visible"] = [
                    value for value in [".env", ".git", "node_modules", ".venv"] if value in names
                ]
                item["ok"] = not item["blocked_visible"]
            elif name in {"search_text", "symbol_search", "git_log"}:
                item["count"] = result.get("count")
            elif name == "repo_info":
                item["project_root"] = result.get("project_root")
                item["git_error"] = result.get("git_error")
                item["ok"] = not result.get("git_error")
            checks.append(item)
        except Exception as exc:  # noqa: BLE001
            checks.append({"name": key, "tool": name, "ok": key == "blocked_policy", "error": str(exc)})
    return {
        **_namespace_info(),
        "project_root": str(settings.project_root),
        "ok": all(item["ok"] for item in checks),
        "checks": checks,
    }


@mcp.tool(
    name="context_bootstrap",
    annotations={**READ_ONLY, "title": "Context Bootstrap"},
)
def context_bootstrap_tool() -> dict:
    """Read the repository's standard context files in one call."""
    paths = [".claude/MEMORY.md", "missions/CURRENT.md", "AGENTS.md", "missions/BACKLOG.md"]
    files = []
    for path in paths:
        try:
            files.append(read_text_file(path=path, settings=settings))
        except FileNotFoundError:
            files.append({"path": path, "missing": True})
        except ValueError as exc:
            if "not a file" in str(exc):
                files.append({"path": path, "missing": True})
            else:
                files.append({"path": path, "error": str(exc)})
        except Exception as exc:  # noqa: BLE001
            files.append({"path": path, "error": str(exc)})
    return {"files": files, "count": len(files)}


@mcp.tool(
    name="batch_call",
    annotations={**READ_ONLY, "title": "Batch Call"},
)
def batch_call_tool(calls: list[dict]) -> dict:
    """Run up to 10 read-only tool calls in one request."""
    if len(calls) > 10:
        raise ValueError("too many calls; max is 10")
    results = []
    for call in calls:
        tool = call.get("tool")
        args = call.get("args") or {}
        if not isinstance(tool, str) or not isinstance(args, dict):
            results.append({"tool": tool, "ok": False, "error": "call must contain string tool and object args"})
            continue
        try:
            results.append({"tool": tool, "ok": True, "result": _batch_dispatch(tool, args)})
        except Exception as exc:  # noqa: BLE001
            results.append({"tool": tool, "ok": False, "error": str(exc)})
    return {"results": results, "count": len(results)}
