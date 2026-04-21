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
def list_dir_tool(path: str = ".", include_hidden: bool = False, limit: int = 200) -> dict:
    """List files and directories under a repo-relative path."""
    return list_dir(path=path, settings=settings, include_hidden=include_hidden, limit=limit)


@mcp.tool(
    name="tree",
    annotations={**READ_ONLY, "title": "Tree"},
)
def tree_tool(path: str = ".", depth: int = 4, include_hidden: bool = False) -> dict:
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
    include_hidden: bool = False,
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
